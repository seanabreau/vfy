use super::{Identity, InvocationCount, Observations, Registration};
use pyo3::Python;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

fn registration(published: bool, counts: &[(&str, u64)]) -> Registration {
    Registration {
        name: "example".to_owned(),
        identity: Ok(Identity {
            module: "example".to_owned(),
            origin: Some("example.py".to_owned()),
            sha256: Some("a".repeat(64)),
        }),
        hooks: vec!["pytest_active".to_owned(), "pytest_unused".to_owned()],
        counts: counts
            .iter()
            .map(|(name, count)| {
                (
                    (*name).to_owned(),
                    Arc::new(InvocationCount(AtomicU64::new(*count))),
                )
            })
            .collect(),
        published,
    }
}

#[test]
fn published_snapshot_covers_uncalled_hooks_without_fabricating_invocations() {
    let state = Observations {
        next_generation: 1,
        registrations: BTreeMap::from([(1, registration(true, &[("pytest_active", 2)]))]),
    };
    let rows = state.snapshot().expect("complete source evidence");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].hook_calls["pytest_active"], 2);
    assert_eq!(rows[0].hook_calls["pytest_unused"], 0);
    assert_eq!(rows[0].hook_calls.len(), 2);
}

#[test]
fn uninvoked_failed_registration_stays_absent_despite_declared_hooks() {
    let mut state = Observations {
        next_generation: 1,
        registrations: BTreeMap::from([(1, registration(false, &[]))]),
    };
    state.declare(1, "pytest_active").expect("declared hook");
    state.declare(1, "pytest_unused").expect("declared hook");
    assert!(
        state
            .snapshot()
            .expect("failed registration evidence")
            .is_empty()
    );
}

#[test]
fn failed_registration_keeps_real_historic_calls_and_zero_uncalled_hooks() {
    let mut state = Observations {
        next_generation: 1,
        registrations: BTreeMap::from([(1, registration(false, &[]))]),
    };
    let invoked = state.declare(1, "pytest_active").expect("historic hook");
    state.declare(1, "pytest_unused").expect("declared hook");
    invoked.increment().expect("historic replay");
    let rows = state.snapshot().expect("historic failure evidence");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].hook_calls["pytest_active"], 1);
    assert_eq!(rows[0].hook_calls["pytest_unused"], 0);
}

#[test]
fn aliases_share_count_and_overflow_leaves_the_observation_unchanged() {
    let mut state = Observations {
        next_generation: 1,
        registrations: BTreeMap::from([(1, registration(true, &[]))]),
    };
    let first = state.declare(1, "pytest_active").expect("registration");
    let second = state
        .declare(1, "pytest_active")
        .expect("same registration");
    first.increment().expect("first alias");
    second.increment().expect("second alias");
    assert_eq!(state.snapshot().unwrap()[0].hook_calls["pytest_active"], 2);

    first.0.store(u64::MAX, Ordering::Relaxed);
    Python::initialize();
    Python::attach(|py| {
        let error = second.increment().expect_err("overflow must not wrap");
        assert!(error.is_instance_of::<pyo3::exceptions::PyOverflowError>(py));
        assert_eq!(
            error.to_string(),
            "OverflowError: hook call count exceeds u64"
        );
    });
    assert_eq!(
        state.snapshot().unwrap()[0].hook_calls["pytest_active"],
        u64::MAX
    );
}
