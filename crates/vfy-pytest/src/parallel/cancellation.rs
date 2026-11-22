//! Signal ownership and bounded cleanup for an authenticated execution drain.

use super::{POLL_INTERVAL, ParallelChild, ParallelSupervisor, is_missing_process_group};
use pyo3::exceptions::PyBaseException;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rustix::process::{Pid, Signal, kill_process_group};
use std::fmt;
use std::io;
use std::os::unix::process::ExitStatusExt as _;
use std::process::ExitStatus;
use std::thread;
use std::time::{Duration, Instant};

pub(super) const DRAIN_TIMEOUT: Duration = Duration::from_secs(5);

fn exception_dictionary<'py>(py: Python<'py>, error: &PyErr) -> PyResult<Bound<'py, PyDict>> {
    py.get_type::<PyBaseException>()
        .getattr("__dict__")?
        .get_item("__dict__")?
        .call_method1("__get__", (error.value(py),))?
        .cast_into::<PyDict>()
        .map_err(Into::into)
}

fn parent_reaches_primary(
    py: Python<'_>,
    parent: &PyErr,
    primary: &std::collections::HashSet<usize>,
) -> bool {
    let mut remaining = vec![parent.clone_ref(py)];
    let mut visited = std::collections::HashSet::new();
    while let Some(error) = remaining.pop() {
        let identity = error.value(py).as_ptr() as usize;
        if primary.contains(&identity) {
            return true;
        }
        if !visited.insert(identity) {
            continue;
        }
        remaining.extend(error.context(py));
        remaining.extend(error.cause(py));
    }
    false
}

/// Chains the parent interruption onto the deepest context link that can take it. The parent
/// is dropped when it already reaches the error or when chaining it would close a cycle.
pub(crate) fn retain_parent_context(py: Python<'_>, error: &PyErr, parent: PyErr) {
    let mut cursor = error.clone_ref(py);
    let mut visited = std::collections::HashSet::new();
    loop {
        let identity = cursor.value(py).as_ptr() as usize;
        if identity == parent.value(py).as_ptr() as usize {
            return;
        }
        if !visited.insert(identity) || parent_reaches_primary(py, &parent, &visited) {
            return;
        }
        if let Some(next) = cursor.context(py) {
            cursor = next;
        } else {
            cursor.set_context(py, Some(parent));
            return;
        }
    }
}

pub(super) fn annotate_execution_failure(
    py: Python<'_>,
    error: &PyErr,
    detail: Option<String>,
    parent: Option<PyErr>,
) {
    if let Some(parent) = parent {
        retain_parent_context(py, error, parent);
    }
    // A failed note is discarded so that it never replaces the primary error.
    if let Some(detail) = detail {
        let _ = retain_cleanup_detail(py, error, &detail);
    }
}

fn retain_cleanup_detail(py: Python<'_>, error: &PyErr, detail: &str) -> PyResult<()> {
    exception_dictionary(py, error)?.set_item("_vfy_cleanup_failures", detail)?;
    // Call the actual BaseException protocol even if a plug-in overrides add_note.
    py.get_type::<PyBaseException>()
        .getattr("add_note")?
        .call1((
            error.value(py),
            format!("native worker cleanup failed: {detail}"),
        ))?;
    Ok(())
}

#[derive(Clone, Copy, Default)]
enum InterruptDelivery {
    #[default]
    Pending,
    Delivered,
    GroupGone,
}

#[derive(Default)]
pub(super) struct Cancellation {
    deliveries: Vec<InterruptDelivery>,
    started: Option<Instant>,
}

#[derive(Clone, Copy)]
pub(super) struct DeliveredInterruption {
    worker: usize,
}

impl DeliveredInterruption {
    pub(super) fn worker(self) -> usize {
        self.worker
    }
}

impl Cancellation {
    pub(super) fn start(&mut self, now: Instant) {
        // Both authenticated worker controls and the parent use this first window.
        if self.started.is_none() {
            self.started = Some(now);
        }
    }

    pub(super) fn expired(&self, now: Instant) -> bool {
        self.started
            .is_some_and(|started| now.duration_since(started) >= DRAIN_TIMEOUT)
    }

    pub(super) fn delivered_interruption(
        &self,
        worker: usize,
        status: ExitStatus,
    ) -> Option<DeliveredInterruption> {
        (self.started.is_some()
            && status.signal() == Some(Signal::INT.as_raw())
            && matches!(
                self.deliveries.get(worker),
                Some(InterruptDelivery::Delivered)
            ))
        .then_some(DeliveredInterruption { worker })
    }

    fn deliver(&mut self, index: usize, send: impl FnOnce() -> io::Result<()>) -> io::Result<()> {
        self.deliveries.resize(
            self.deliveries.len().max(index + 1),
            InterruptDelivery::Pending,
        );
        if !matches!(self.deliveries[index], InterruptDelivery::Pending) {
            return Ok(());
        }
        self.deliveries[index] = match send() {
            Ok(()) => InterruptDelivery::Delivered,
            Err(error) if error.raw_os_error() == Some(rustix::io::Errno::SRCH.raw_os_error()) => {
                InterruptDelivery::GroupGone
            }
            Err(error) => return Err(error),
        };
        Ok(())
    }
}

#[derive(Debug)]
struct CleanupFailure {
    index: usize,
    operation: &'static str,
    error: io::Error,
}

#[derive(Debug)]
pub(super) struct CleanupOutcome {
    failures: Vec<CleanupFailure>,
    pending_groups: Vec<usize>,
    deadline: Instant,
}

impl CleanupOutcome {
    pub(super) fn succeeded(&self) -> bool {
        self.failures.is_empty()
    }

    fn record(&mut self, index: usize, operation: &'static str, error: io::Error) {
        self.failures.push(CleanupFailure {
            index,
            operation,
            error,
        });
    }
}

impl fmt::Display for CleanupOutcome {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (position, failure) in self.failures.iter().enumerate() {
            if position != 0 {
                output.write_str("; ")?;
            }
            write!(
                output,
                "worker {} {}: {}",
                failure.index, failure.operation, failure.error
            )?;
        }
        Ok(())
    }
}

// This seam permits failure tests without exposing process-operation injection to Python.
trait CleanupProcesses {
    fn width(&self) -> usize;
    fn owned(&self, index: usize) -> bool;
    fn kill_group(&mut self, index: usize) -> io::Result<()>;
    fn kill_child(&mut self, index: usize) -> io::Result<()>;
    fn poll_child(&mut self, index: usize) -> io::Result<bool>;
}

fn cleanup(processes: &mut impl CleanupProcesses, deadline: Instant) -> CleanupOutcome {
    let groups = (0..processes.width()).collect::<Vec<_>>();
    cleanup_pending(processes, deadline, &groups)
}

fn retry_cleanup(
    processes: &mut impl CleanupProcesses,
    mut previous: CleanupOutcome,
) -> CleanupOutcome {
    let retried = cleanup_pending(processes, previous.deadline, &previous.pending_groups);
    previous.failures.extend(retried.failures);
    previous.pending_groups = retried.pending_groups;
    previous
}

fn cleanup_pending(
    processes: &mut impl CleanupProcesses,
    deadline: Instant,
    groups: &[usize],
) -> CleanupOutcome {
    let mut outcome = CleanupOutcome {
        failures: Vec::new(),
        pending_groups: Vec::new(),
        deadline,
    };
    let width = processes.width();
    let mut unpollable = vec![false; width];
    for &index in groups {
        if let Err(error) = processes.kill_group(index) {
            outcome.pending_groups.push(index);
            outcome.record(index, "process group kill", error);
        }
    }
    for (index, failed) in unpollable.iter_mut().enumerate() {
        if !processes.owned(index) {
            continue;
        }
        match processes.poll_child(index) {
            Ok(true) => continue,
            Ok(false) => (),
            Err(error) => {
                *failed = true;
                outcome.record(index, "child wait", error);
            }
        }
        if let Err(error) = processes.kill_child(index) {
            outcome.record(index, "child kill", error);
        }
    }
    loop {
        let mut pending = false;
        for (index, failed) in unpollable.iter_mut().enumerate() {
            if !processes.owned(index) || *failed {
                continue;
            }
            match processes.poll_child(index) {
                Ok(true) => (),
                Ok(false) => pending = true,
                Err(error) => {
                    *failed = true;
                    outcome.record(index, "child wait", error);
                }
            }
        }
        if !pending || Instant::now() >= deadline {
            break;
        }
        thread::sleep(POLL_INTERVAL);
    }
    for index in 0..width {
        if processes.owned(index) {
            outcome.record(
                index,
                "reap deadline",
                io::Error::other("child exit was not reaped"),
            );
        }
    }
    outcome
}

#[cfg(any(target_os = "macos", test))]
fn group_leader_reaped(child: &mut Option<ParallelChild>) -> io::Result<bool> {
    match child {
        None => Ok(true),
        Some(ParallelChild::Spawned(child)) => Ok(child.try_wait()?.is_some()),
        // waitpid consumes a forked child's status; the supervisor owns that wait.
        Some(ParallelChild::Forked(_)) => Ok(false),
    }
}

// Darwin can deny a group signal while the exiting leader is not yet waitable.
// Preserve its exit for the normal completion-validation loop; only ESRCH from
// the non-signalling probe proves the whole group disappeared after reaping.
#[cfg(any(target_os = "macos", test))]
fn confirm_group_absent(
    error: io::Error,
    mut reap: impl FnMut() -> io::Result<bool>,
    mut probe: impl FnMut() -> rustix::io::Result<()>,
) -> io::Result<()> {
    if error.raw_os_error() != Some(rustix::io::Errno::PERM.raw_os_error()) {
        return Err(error);
    }
    let deadline = Instant::now() + Duration::from_millis(100);
    loop {
        match reap() {
            Ok(true) => break,
            Ok(false) if Instant::now() < deadline => thread::sleep(POLL_INTERVAL),
            Ok(false) | Err(_) => return Err(error),
        }
    }
    loop {
        match probe() {
            Err(rustix::io::Errno::SRCH) => return Err(io::Error::from(rustix::io::Errno::SRCH)),
            Ok(()) | Err(rustix::io::Errno::PERM) if Instant::now() < deadline => {
                thread::sleep(POLL_INTERVAL);
            }
            _ => return Err(error),
        }
    }
}

pub(super) fn signal_group(
    child: &mut Option<ParallelChild>,
    group: &mut Option<Pid>,
    signal: Signal,
) -> io::Result<()> {
    let Some(pid) = *group else {
        return Err(io::Error::from(rustix::io::Errno::SRCH));
    };
    let result = kill_process_group(pid, signal).map_err(io::Error::from);
    #[cfg(target_os = "macos")]
    let result = {
        if let Err(error) = result {
            confirm_group_absent(
                error,
                || group_leader_reaped(child),
                || rustix::process::test_kill_process_group(pid),
            )
        } else {
            Ok(())
        }
    };
    #[cfg(not(target_os = "macos"))]
    let result = {
        let _ = child;
        result
    };
    if result
        .as_ref()
        .is_err_and(|error| error.raw_os_error() == Some(rustix::io::Errno::SRCH.raw_os_error()))
    {
        *group = None;
    }
    result
}

impl ParallelSupervisor<'_> {
    pub(super) fn interrupt_execution_peers(&mut self) -> io::Result<()> {
        self.cancellation.start(Instant::now());
        for index in 0..self.children.len() {
            if self.children[index].is_some() && self.process_groups[index].is_some() {
                self.cancellation.deliver(index, || {
                    signal_group(
                        &mut self.children[index],
                        &mut self.process_groups[index],
                        Signal::INT,
                    )
                })?;
            }
        }
        Ok(())
    }

    pub(super) fn retry_execution_cleanup(&mut self) {
        self.checked_cleanup = Some(match self.checked_cleanup.take() {
            Some(previous) if !previous.succeeded() => retry_cleanup(self, previous),
            Some(previous) => previous,
            None => cleanup(self, Instant::now() + DRAIN_TIMEOUT),
        });
    }

    pub(super) fn cleanup_execution(&mut self) -> Option<String> {
        if self.checked_cleanup.is_none() {
            // No Python signals or callbacks run while process ownership is being discharged.
            self.checked_cleanup = Some(cleanup(self, Instant::now() + DRAIN_TIMEOUT));
        }
        self.checked_cleanup
            .as_ref()
            .filter(|outcome| !outcome.succeeded())
            .map(ToString::to_string)
    }
}

impl CleanupProcesses for ParallelSupervisor<'_> {
    fn width(&self) -> usize {
        self.children.len()
    }
    fn owned(&self, index: usize) -> bool {
        self.children[index].is_some()
    }
    fn kill_group(&mut self, index: usize) -> io::Result<()> {
        if self.process_groups[index].is_none() {
            return Ok(());
        }
        match signal_group(
            &mut self.children[index],
            &mut self.process_groups[index],
            Signal::KILL,
        ) {
            Err(error) if error.raw_os_error() == Some(rustix::io::Errno::SRCH.raw_os_error()) => {
                Ok(())
            }
            result => result,
        }
    }
    fn kill_child(&mut self, index: usize) -> io::Result<()> {
        let Some(child) = self.children[index].as_mut() else {
            return Ok(());
        };
        match child.kill() {
            Err(error)
                if error.raw_os_error().is_some_and(|code| {
                    is_missing_process_group(rustix::io::Errno::from_raw_os_error(code))
                }) =>
            {
                Ok(())
            }
            result => result,
        }
    }
    fn poll_child(&mut self, index: usize) -> io::Result<bool> {
        let status = self.children[index]
            .as_mut()
            .map(ParallelChild::try_wait)
            .transpose()?
            .flatten();
        if let Some(status) = status {
            self.statuses[index] = Some(status);
            self.children[index] = None;
            Ok(true)
        } else {
            Ok(self.children[index].is_none())
        }
    }
}

#[cfg(test)]
mod tests;
