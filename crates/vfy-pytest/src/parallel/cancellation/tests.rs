use super::*;
use std::cell::Cell;

#[test]
fn only_actual_interrupt_delivery_can_authorize_a_completed_signal_status() {
    let mut cancellation = Cancellation::default();
    let interrupted = ExitStatus::from_raw(Signal::INT.as_raw());
    assert!(
        cancellation
            .delivered_interruption(0, interrupted)
            .is_none()
    );
    cancellation.start(Instant::now());
    cancellation
        .deliver(0, || Ok(()))
        .expect("successful controller interrupt delivery");
    assert_eq!(
        cancellation
            .delivered_interruption(0, interrupted)
            .expect("the actual controller delivered this interrupt")
            .worker(),
        0
    );
    for (worker, status) in [
        (1, interrupted),
        (0, ExitStatus::from_raw(42 << 8)),
        (0, ExitStatus::from_raw(Signal::KILL.as_raw())),
    ] {
        assert!(
            cancellation
                .delivered_interruption(worker, status)
                .is_none()
        );
    }
    cancellation
        .deliver(1, || Err(io::Error::from(rustix::io::Errno::SRCH)))
        .expect("a process group that vanished was not interrupted");
    assert!(
        cancellation
            .delivered_interruption(1, interrupted)
            .is_none()
    );
    assert!(
        cancellation
            .deliver(2, || Err(io::Error::other("actual denied signal")))
            .is_err()
    );
    assert!(
        cancellation
            .delivered_interruption(2, interrupted)
            .is_none()
    );
}

#[test]
fn worker_then_parent_keeps_first_deadline_and_group_signal() {
    let mut cancellation = Cancellation::default();
    let first = Instant::now();
    let sends = Cell::new(0);
    cancellation.start(first);
    cancellation
        .deliver(0, || {
            sends.set(sends.get() + 1);
            Ok(())
        })
        .expect("first signal");
    cancellation.start(first + Duration::from_secs(4));
    cancellation
        .deliver(0, || {
            sends.set(sends.get() + 1);
            Ok(())
        })
        .expect("adopt worker signal");
    cancellation
        .deliver(1, || {
            sends.set(sends.get() + 1);
            Ok(())
        })
        .expect("remaining peer");
    assert_eq!(sends.get(), 2);
    assert!(cancellation.expired(first + DRAIN_TIMEOUT));
}

#[test]
fn failed_signal_retains_ownership_until_actual_delivery() {
    let mut cancellation = Cancellation::default();
    assert!(
        cancellation
            .deliver(0, || Err(io::Error::other("denied")))
            .is_err()
    );
    cancellation
        .deliver(0, || Ok(()))
        .expect("retry actual operation");
    cancellation
        .deliver(0, || panic!("delivered group signalled twice"))
        .expect("no second signal");
}

#[test]
fn cleanup_error_context_keeps_primary_cause_and_first_parent() {
    Python::initialize();
    Python::attach(|py| {
        let error = pyo3::exceptions::PyValueError::new_err("actual coverage failure");
        let cause = pyo3::exceptions::PyOSError::new_err("original operation cause");
        let previous = pyo3::exceptions::PyRuntimeError::new_err("existing context");
        let parent = pyo3::exceptions::PyKeyboardInterrupt::new_err("first parent");
        error.set_cause(py, Some(cause.clone_ref(py)));
        error.set_context(py, Some(previous.clone_ref(py)));
        retain_parent_context(py, &error, parent.clone_ref(py));
        retain_cleanup_detail(py, &error, "worker 0 child wait: denied")
            .expect("retain cleanup failure");
        assert!(
            error
                .cause(py)
                .expect("original cause")
                .value(py)
                .is(cause.value(py))
        );
        assert!(
            error
                .context(py)
                .expect("existing context")
                .value(py)
                .is(previous.value(py))
        );
        assert!(
            previous
                .context(py)
                .expect("parent context")
                .value(py)
                .is(parent.value(py))
        );
        assert!(
            exception_dictionary(py, &error)
                .expect("actual exception dictionary")
                .contains("_vfy_cleanup_failures")
                .expect("cleanup detail")
        );
    });
}

#[test]
fn preexisting_context_cycle_retains_parent_without_rewriting_cycle() {
    Python::initialize();
    Python::attach(|py| {
        let error = pyo3::exceptions::PyValueError::new_err("primary");
        let previous = pyo3::exceptions::PyRuntimeError::new_err("existing context");
        let parent = pyo3::exceptions::PyKeyboardInterrupt::new_err("first parent");
        error.set_context(py, Some(previous.clone_ref(py)));
        previous.set_context(py, Some(error.clone_ref(py)));
        retain_parent_context(py, &error, parent.clone_ref(py));
        assert!(
            previous
                .context(py)
                .expect("cycle retained")
                .value(py)
                .is(error.value(py))
        );
        assert!(
            parent.context(py).is_none() && error.context(py).is_some(),
            "a parent that would close a cycle is not chained"
        );
        // Release the deliberate cycle even when the embedding interpreter persists.
        previous.set_context(py, None);
    });
}

struct FailedCleanup {
    owned: Vec<bool>,
    group_attempts: Vec<usize>,
    child_attempts: Vec<usize>,
}
impl CleanupProcesses for FailedCleanup {
    fn width(&self) -> usize {
        self.owned.len()
    }
    fn owned(&self, index: usize) -> bool {
        self.owned[index]
    }
    fn kill_group(&mut self, index: usize) -> io::Result<()> {
        self.group_attempts.push(index);
        if index == 0 {
            Err(io::Error::other("group denied"))
        } else {
            Ok(())
        }
    }
    fn kill_child(&mut self, index: usize) -> io::Result<()> {
        self.child_attempts.push(index);
        if index == 0 {
            Err(io::Error::other("child denied"))
        } else {
            Ok(())
        }
    }
    fn poll_child(&mut self, index: usize) -> io::Result<bool> {
        if index == 0 {
            Err(io::Error::other("wait denied"))
        } else {
            self.owned[index] = false;
            Ok(true)
        }
    }
}
#[test]
fn cleanup_attempts_all_groups_and_retains_unreaped_failed_child() {
    let mut processes = FailedCleanup {
        owned: vec![true, true],
        group_attempts: Vec::new(),
        child_attempts: Vec::new(),
    };
    let outcome = cleanup(&mut processes, Instant::now());
    assert!(!outcome.succeeded());
    assert_eq!(processes.group_attempts, [0, 1]);
    assert_eq!(processes.child_attempts, [0]);
    assert_eq!(processes.owned, [true, false]);
    assert_eq!(outcome.failures.len(), 4);
}

#[test]
fn parent_context_reaching_primary_or_its_tail_is_not_appended() {
    Python::initialize();
    Python::attach(|py| {
        for reaches_tail in [false, true] {
            let primary = pyo3::exceptions::PyValueError::new_err("primary worker failure");
            let cause = pyo3::exceptions::PyOSError::new_err("original cause");
            let tail = pyo3::exceptions::PyRuntimeError::new_err("existing primary context");
            let parent = pyo3::exceptions::PyKeyboardInterrupt::new_err("first parent");
            primary.set_cause(py, Some(cause.clone_ref(py)));
            primary.set_context(py, Some(tail.clone_ref(py)));
            let target = if reaches_tail { &tail } else { &primary };
            parent.set_context(py, Some(target.clone_ref(py)));
            retain_parent_context(py, &primary, parent.clone_ref(py));
            assert!(
                primary
                    .context(py)
                    .expect("unchanged primary context")
                    .value(py)
                    .is(tail.value(py))
            );
            assert!(
                tail.context(py).is_none(),
                "new context cycle was introduced"
            );
            assert!(
                primary
                    .cause(py)
                    .expect("unchanged cause")
                    .value(py)
                    .is(cause.value(py))
            );
            assert!(
                parent
                    .context(py)
                    .expect("unchanged parent context")
                    .value(py)
                    .is(target.value(py))
            );
            parent.set_context(py, None);
        }
    });
}

#[test]
fn malformed_notes_keep_actual_primary_cause_cleanup_and_parent() {
    Python::initialize();
    Python::attach(|py| {
        let primary = pyo3::exceptions::PyValueError::new_err("actual authentication failure");
        let cause = pyo3::exceptions::PyOSError::new_err("original authentication cause");
        let parent = pyo3::exceptions::PyKeyboardInterrupt::new_err("first parent");
        primary.set_cause(py, Some(cause.clone_ref(py)));
        primary
            .value(py)
            .setattr("__notes__", 42)
            .expect("permitted hostile notes value");
        let identity = primary.value(py).clone().unbind();
        let mut processes = FailedCleanup {
            owned: vec![true],
            group_attempts: Vec::new(),
            child_attempts: Vec::new(),
        };
        let cleanup = cleanup(&mut processes, Instant::now());
        assert!(!cleanup.succeeded());
        annotate_execution_failure(
            py,
            &primary,
            Some(cleanup.to_string()),
            Some(parent.clone_ref(py)),
        );
        assert!(primary.value(py).is(identity.bind(py)));
        assert!(
            primary
                .cause(py)
                .expect("original cause")
                .value(py)
                .is(cause.value(py))
        );
        assert!(
            primary
                .context(py)
                .expect("first parent context")
                .value(py)
                .is(parent.value(py))
        );
        assert_eq!(
            primary
                .value(py)
                .getattr("__notes__")
                .expect("unchanged hostile notes")
                .extract::<i32>()
                .expect("integer notes"),
            42
        );
        let dictionary = exception_dictionary(py, &primary).expect("actual exception dictionary");
        assert_eq!(
            dictionary
                .get_item("_vfy_cleanup_failures")
                .expect("cleanup lookup")
                .expect("actual cleanup details")
                .extract::<String>()
                .expect("cleanup diagnostic"),
            cleanup.to_string()
        );
    });
}

struct TransientCleanup {
    failed: bool,
    owned: bool,
    group_attempts: usize,
    wait_attempts: usize,
    kill_attempts: usize,
}

impl CleanupProcesses for TransientCleanup {
    fn width(&self) -> usize {
        1
    }
    fn owned(&self, _index: usize) -> bool {
        self.owned
    }
    fn kill_group(&mut self, _index: usize) -> io::Result<()> {
        self.group_attempts += 1;
        if self.failed {
            Err(io::Error::other("transient group failure"))
        } else {
            Ok(())
        }
    }
    fn kill_child(&mut self, _index: usize) -> io::Result<()> {
        self.kill_attempts += 1;
        if self.failed {
            Err(io::Error::other("transient child failure"))
        } else {
            Ok(())
        }
    }
    fn poll_child(&mut self, _index: usize) -> io::Result<bool> {
        self.wait_attempts += 1;
        if self.failed {
            Err(io::Error::other("transient reap failure"))
        } else {
            self.owned = false;
            Ok(true)
        }
    }
}

#[test]
fn final_cleanup_retry_discharges_transient_ownership_and_retains_failure_and_deadline() {
    let mut processes = TransientCleanup {
        failed: true,
        owned: true,
        group_attempts: 0,
        wait_attempts: 0,
        kill_attempts: 0,
    };
    let deadline = Instant::now();
    let initial = cleanup(&mut processes, deadline);
    let original_failures = initial.to_string();
    assert!(processes.owned);
    assert_eq!(initial.pending_groups, [0]);
    processes.failed = false;
    let retried = retry_cleanup(&mut processes, initial);
    assert!(!processes.owned);
    assert!(retried.pending_groups.is_empty());
    assert_eq!(processes.group_attempts, 2);
    assert_eq!(processes.wait_attempts, 2);
    assert_eq!(processes.kill_attempts, 1);
    assert_eq!(retried.deadline, deadline);
    assert_eq!(retried.to_string(), original_failures);
    assert!(
        !retried.succeeded(),
        "a final retry must not authorize an incomplete run"
    );
}

#[test]
fn final_cleanup_retry_retains_permanent_failed_ownership_and_all_errors() {
    let mut processes = FailedCleanup {
        owned: vec![true, true],
        group_attempts: Vec::new(),
        child_attempts: Vec::new(),
    };
    let deadline = Instant::now();
    let initial = cleanup(&mut processes, deadline);
    let prior = initial.to_string();
    let retried = retry_cleanup(&mut processes, initial);
    assert_eq!(processes.owned, [true, false]);
    assert_eq!(processes.group_attempts, [0, 1, 0]);
    assert_eq!(processes.child_attempts, [0, 0]);
    assert_eq!(retried.pending_groups, [0]);
    assert_eq!(retried.deadline, deadline);
    assert!(retried.to_string().starts_with(&prior));
    assert_eq!(retried.failures.len(), 8);
    assert!(!retried.succeeded());
}

#[test]
fn darwin_reaped_group_must_be_absent_to_resolve_permission_error() {
    let denied = || io::Error::from(rustix::io::Errno::PERM);
    let absent = || Err(rustix::io::Errno::SRCH);
    let err = confirm_group_absent(denied(), || Ok(true), absent).unwrap_err();
    assert_eq!(
        err.raw_os_error(),
        Some(rustix::io::Errno::SRCH.raw_os_error())
    );
    let mut probes = [Err(rustix::io::Errno::PERM), Ok(()), absent()].into_iter();
    let error = confirm_group_absent(
        denied(),
        || group_leader_reaped(&mut None),
        || {
            probes
                .next()
                .expect("stop probing once the group disappears")
        },
    )
    .unwrap_err();
    assert_eq!(
        error.raw_os_error(),
        Some(rustix::io::Errno::SRCH.raw_os_error())
    );
    let mut waits = [false, true].into_iter();
    let error = confirm_group_absent(denied(), || Ok(waits.next().unwrap()), absent).unwrap_err();
    assert_eq!(
        error.raw_os_error(),
        Some(rustix::io::Errno::SRCH.raw_os_error())
    );
    for (reaped, probe) in [
        (false, Err(rustix::io::Errno::SRCH)),
        (true, Ok(())),
        (true, Err(rustix::io::Errno::PERM)),
    ] {
        let err = confirm_group_absent(denied(), || Ok(reaped), || probe).unwrap_err();
        assert_eq!(
            err.raw_os_error(),
            Some(rustix::io::Errno::PERM.raw_os_error())
        );
    }
    let error = confirm_group_absent(denied(), || Err(io::Error::other("wait failed")), absent)
        .unwrap_err();
    assert_eq!(
        error.raw_os_error(),
        Some(rustix::io::Errno::PERM.raw_os_error())
    );
    let error = confirm_group_absent(
        io::Error::from(rustix::io::Errno::INVAL),
        || panic!("must not reap"),
        || panic!("must not probe"),
    )
    .unwrap_err();
    assert_eq!(
        error.raw_os_error(),
        Some(rustix::io::Errno::INVAL.raw_os_error())
    );
}

#[test]
fn spawned_child_retains_exit_status_after_a_nonblocking_reap() {
    let child = std::process::Command::new("sh")
        .args(["-c", "exit 7"])
        .spawn()
        .expect("start child");
    let mut child = ParallelChild::Spawned(child);
    let deadline = Instant::now() + Duration::from_secs(5);
    let first = loop {
        if let Some(status) = child.try_wait().expect("poll child") {
            break status;
        }
        assert!(Instant::now() < deadline, "child did not exit");
        thread::yield_now();
    };
    assert_eq!(first.code(), Some(7));
    assert_eq!(child.try_wait().expect("poll child again"), Some(first));
}
