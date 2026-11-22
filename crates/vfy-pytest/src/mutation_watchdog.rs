//! Supervise fresh mutation children while Python owns pytest collection.

#[cfg(target_os = "linux")]
use std::collections::BTreeSet;
use std::io;
#[cfg(target_os = "linux")]
use std::path::Path;
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex};
#[cfg(target_os = "linux")]
use std::thread;
use std::thread::JoinHandle;
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

use pyo3::exceptions::PyOSError;
#[cfg(not(target_os = "linux"))]
use pyo3::exceptions::PyRuntimeError;
#[cfg(target_os = "linux")]
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
#[cfg(target_os = "linux")]
use rustix::process::{Pid, Signal, kill_process_group};
#[cfg(target_os = "linux")]
use rustix::process::{WaitId, WaitIdOptions, waitid};

#[cfg(target_os = "linux")]
const POLL_INTERVAL: Duration = Duration::from_millis(20);
#[cfg(target_os = "linux")]
const MAX_JOBS: usize = 256;

type StopSignal = Arc<(Mutex<bool>, Condvar)>;

#[cfg(target_os = "linux")]
struct WatchedProcess {
    pid: Pid,
    deadline: Instant,
}

/// Owns no Python references and never invokes Python from its kernel watcher.
#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct MutationProcessWatchdog {
    owner: u32,
    stop: StopSignal,
    thread: Option<JoinHandle<io::Result<Vec<i32>>>>,
    expired: Vec<i32>,
}

#[pymethods]
impl MutationProcessWatchdog {
    #[staticmethod]
    const fn available() -> bool {
        cfg!(target_os = "linux")
    }

    #[new]
    #[pyo3(signature = (jobs, pool_directory=None))]
    fn new(jobs: Vec<(i32, f64)>, pool_directory: Option<PathBuf>) -> PyResult<Self> {
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (jobs, pool_directory);
            Err(PyRuntimeError::new_err(
                "native mutation watchdog requires Linux waitid",
            ))
        }
        #[cfg(target_os = "linux")]
        {
            if jobs.len() > MAX_JOBS {
                return Err(PyValueError::new_err(
                    "mutation watchdog supports at most 256 jobs",
                ));
            }
            let now = Instant::now();
            let mut watched = Vec::with_capacity(jobs.len());
            let mut identities = BTreeSet::new();
            for (raw_pid, seconds) in jobs {
                let pid = Pid::from_raw(raw_pid)
                    .filter(|_| raw_pid > 1)
                    .ok_or_else(|| {
                        PyValueError::new_err("mutation watchdog requires positive child PIDs")
                    })?;
                if !identities.insert(raw_pid) {
                    return Err(PyValueError::new_err(
                        "mutation watchdog received a duplicate child PID",
                    ));
                }
                let duration = Duration::try_from_secs_f64(seconds).map_err(|_| {
                    PyValueError::new_err(
                        "mutation watchdog deadline must be finite and nonnegative",
                    )
                })?;
                let deadline = now.checked_add(duration).ok_or_else(|| {
                    PyValueError::new_err("mutation watchdog deadline is too large")
                })?;
                watched.push(WatchedProcess { pid, deadline });
            }
            let stop = Arc::new((Mutex::new(false), Condvar::new()));
            let thread_stop = Arc::clone(&stop);
            let thread = thread::Builder::new()
                .name("vfy-mutation-watchdog".to_owned())
                .spawn(move || watch(watched, pool_directory.as_deref(), &thread_stop))
                .map_err(|error| PyOSError::new_err(error.to_string()))?;
            Ok(Self {
                owner: std::process::id(),
                stop,
                thread: Some(thread),
                expired: Vec::new(),
            })
        }
    }

    fn finish(&mut self, py: Python<'_>) -> PyResult<Vec<i32>> {
        py.detach(|| self.finish_inner())
            .map_err(|error| PyOSError::new_err(error.to_string()))
    }
}

impl MutationProcessWatchdog {
    fn finish_inner(&mut self) -> io::Result<Vec<i32>> {
        if std::process::id() != self.owner {
            // User collection can fork. Its child inherits no running Rust
            // watcher and must never touch a mutex held by that vanished thread.
            std::mem::forget(self.thread.take());
            return Ok(Vec::new());
        }
        let (lock, wake) = &*self.stop;
        // The lock protects only a stop bit; recovering it cannot expose user state.
        *lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = true;
        wake.notify_all();
        if let Some(thread) = self.thread.take() {
            self.expired = thread
                .join()
                .map_err(|_| io::Error::other("native mutation watchdog thread panicked"))??;
        }
        Ok(self.expired.clone())
    }
}

impl Drop for MutationProcessWatchdog {
    fn drop(&mut self) {
        let _ = self.finish_inner();
    }
}

#[cfg(target_os = "linux")]
fn kill_group(pid: Pid) -> io::Result<()> {
    match kill_process_group(pid, Signal::KILL) {
        Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

#[cfg(target_os = "linux")]
fn cleanup_groups(pid: Pid, pool_directory: Option<&Path>) -> io::Result<()> {
    kill_group(pid)?;
    let Some(directory) = pool_directory else {
        return Ok(());
    };
    let entries = match std::fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let prefix = format!("group-{}-", pid.as_raw_pid());
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        let Some(raw) = name.to_str().and_then(|name| name.strip_prefix(&prefix)) else {
            continue;
        };
        let group = raw
            .parse::<i32>()
            .ok()
            .filter(|pid| *pid > 1)
            .and_then(Pid::from_raw)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid mutation file-group PID",
                )
            })?;
        kill_group(group)?;
        match std::fs::remove_file(entry.path()) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn watch(
    mut jobs: Vec<WatchedProcess>,
    pool: Option<&Path>,
    stop: &StopSignal,
) -> io::Result<Vec<i32>> {
    let mut expired = Vec::new();
    while !jobs.is_empty() {
        let mut pending = Vec::with_capacity(jobs.len());
        for job in jobs {
            let status = loop {
                match waitid(
                    WaitId::Pid(job.pid),
                    WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
                ) {
                    Err(rustix::io::Errno::INTR) => {}
                    // Collection code reaped the child. Its PID may already
                    // name another process, so leave cleanup to the owner.
                    Err(rustix::io::Errno::CHILD) => break None,
                    result => break Some(result.map_err(io::Error::from)?),
                }
            };
            let Some(status) = status else {
                continue;
            };
            if status.is_some() {
                cleanup_groups(job.pid, pool)?;
            } else if Instant::now() >= job.deadline {
                cleanup_groups(job.pid, pool)?;
                expired.push(job.pid.as_raw_pid());
            } else {
                pending.push(job);
            }
        }
        jobs = pending;
        let (lock, wake) = &**stop;
        let stopped = lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if *stopped || jobs.is_empty() {
            break;
        }
        let (stopped, _) = wake
            .wait_timeout(stopped, POLL_INTERVAL)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if *stopped {
            break;
        }
    }
    Ok(expired)
}

#[cfg(all(test, target_os = "linux"))]
mod tests;
