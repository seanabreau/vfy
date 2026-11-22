//! Worker child handles, fork resource checks, and process-exit helpers.

use super::io_error;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rustix::process::{Pid, Signal, WaitOptions, kill_process, kill_process_group, waitpid};
use std::collections::HashMap;
use std::fs;
use std::io;
#[cfg(target_os = "linux")]
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{Child, ExitStatus};
use std::time::Duration;

pub(super) enum ParallelChild {
    Spawned(Child),
    Forked(Pid),
}

impl ParallelChild {
    pub(super) fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        match self {
            Self::Spawned(child) => child.try_wait(),
            Self::Forked(pid) => wait_forked(*pid, WaitOptions::NOHANG),
        }
    }

    pub(super) fn kill(&mut self) -> io::Result<()> {
        match self {
            Self::Spawned(child) => child.kill(),
            Self::Forked(pid) => kill_process(*pid, Signal::KILL).map_err(io::Error::from),
        }
    }

    pub(super) fn wait(&mut self) -> io::Result<ExitStatus> {
        match self {
            Self::Spawned(child) => child.wait(),
            Self::Forked(pid) => wait_forked(*pid, WaitOptions::empty())?
                .ok_or_else(|| io::Error::other("blocking fork wait returned no child")),
        }
    }
}

fn wait_forked(pid: Pid, options: WaitOptions) -> io::Result<Option<ExitStatus>> {
    loop {
        match waitpid(Some(pid), options) {
            Ok(status) => {
                return Ok(status.map(|(_, status)| ExitStatus::from_raw(status.as_raw())));
            }
            Err(rustix::io::Errno::INTR) => {}
            Err(error) => return Err(io::Error::from(error)),
        }
    }
}

pub(super) fn collected_fork_timeout() -> PyResult<Duration> {
    match std::env::var("VFY_FORK_STARTUP_SECONDS") {
        Err(std::env::VarError::NotPresent) => Ok(Duration::from_mins(2)),
        Ok(value) => match value.parse::<u64>() {
            Ok(seconds @ 1..=300) => Ok(Duration::from_secs(seconds)),
            _ => Err(PyValueError::new_err(
                "VFY_FORK_STARTUP_SECONDS must be an integer from 1 to 300",
            )),
        },
        Err(error) => Err(PyValueError::new_err(error.to_string())),
    }
}

pub(super) fn read_fork_children(proc_root: &Path, parent: u32) -> io::Result<Vec<i32>> {
    let children_path = proc_root.join(format!("self/task/{parent}/children"));
    match fs::read_to_string(children_path) {
        Ok(children) => {
            return children
                .split_whitespace()
                .map(|child| {
                    child
                        .parse::<i32>()
                        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
                })
                .collect();
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    // Some procfs mounts omit task/children. PPid still identifies live children.
    let mut children = Vec::new();
    for entry in fs::read_dir(proc_root)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name
            .to_str()
            .filter(|name| !name.is_empty() && name.bytes().all(|byte| byte.is_ascii_digit()))
        else {
            continue;
        };
        let pid = name
            .parse::<i32>()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let status = match fs::read_to_string(entry.path().join("status")) {
            Ok(status) => status,
            // A proc inode can survive its process until an already-open status
            // file is read. Linux then returns ESRCH instead of ENOENT.
            Err(error)
                if error.kind() == io::ErrorKind::NotFound
                    || error.raw_os_error() == Some(rustix::io::Errno::SRCH.raw_os_error()) =>
            {
                continue;
            }
            Err(error) => return Err(error),
        };
        let parent_id = status
            .lines()
            .find_map(|line| line.strip_prefix("PPid:"))
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "proc status has no PPid"))?
            .trim()
            .parse::<u32>()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if parent_id == parent {
            children.push(pid);
        }
    }
    Ok(children)
}

pub(crate) fn validate_fork_resources(allowed_fds: &[i32], owned_children: &[Pid]) -> PyResult<()> {
    if !cfg!(target_os = "linux") {
        return Err(PyValueError::new_err(
            "collected fork workers require Linux",
        ));
    }
    let tasks = fs::read_dir("/proc/self/task")
        .map_err(io_error)?
        .map(|entry| entry.map(|_| ()))
        .collect::<io::Result<Vec<_>>>()
        .map_err(io_error)?;
    if tasks.len() != 1 {
        return Err(PyValueError::new_err(
            "collected fork workers require exactly one OS thread",
        ));
    }
    for child in read_fork_children(Path::new("/proc"), std::process::id()).map_err(io_error)? {
        if !owned_children.iter().any(|pid| pid.as_raw_pid() == child) {
            return Err(PyValueError::new_err(
                "collected fork workers reject active foreign subprocesses",
            ));
        }
    }
    let descriptors = fs::read_dir("/proc/self/fd")
        .map_err(io_error)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<Vec<_>>>()
        .map_err(io_error)?;
    for descriptor in descriptors {
        let fd = descriptor
            .file_name()
            .ok_or_else(|| PyValueError::new_err("invalid descriptor path"))?
            .to_string_lossy()
            .parse::<i32>()
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        if !allowed_fds.contains(&fd)
            && fs::read_link(&descriptor).is_ok()
            && !is_entropy_reader(&descriptor, fd)
        {
            return Err(PyValueError::new_err(format!(
                "collected fork workers reject unowned open descriptor {fd}"
            )));
        }
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn is_entropy_reader(descriptor: &Path, fd: i32) -> bool {
    use rustix::fs::{OFlags, makedev};

    let Ok(opened) = fs::metadata(descriptor) else {
        return false;
    };
    // CPython's urandom fallback caches a read-only, close-on-exec descriptor
    // for Linux's 1:9 character device. Its reads have no shared seek position.
    if !opened.file_type().is_char_device() || opened.rdev() != makedev(1, 9) {
        return false;
    }
    fs::read_to_string(format!("/proc/self/fdinfo/{fd}")).is_ok_and(|info| {
        info.lines()
            .find_map(|line| line.strip_prefix("flags:"))
            .and_then(|flags| u32::from_str_radix(flags.trim(), 8).ok())
            .map(OFlags::from_bits_retain)
            .is_some_and(|flags| {
                flags.contains(OFlags::CLOEXEC) && !flags.intersects(OFlags::ACCMODE | OFlags::PATH)
            })
    })
}

#[cfg(not(target_os = "linux"))]
fn is_entropy_reader(_descriptor: &Path, _fd: i32) -> bool {
    false
}

pub(super) fn coverage_base(environment: &HashMap<String, String>) -> Option<&str> {
    environment
        .get("VFY_NATIVE_PARALLEL_COVERAGE_BASE")
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

/// Stop a worker that was started speculatively and never adopted.
pub(super) fn stop_prespawned(pid: Pid, mut child: Child) {
    let _ = kill_process_group(pid, Signal::KILL);
    let _ = child.kill();
    let _ = child.wait();
}

pub(super) fn worker_exit_code(status: ExitStatus) -> i32 {
    match status.code() {
        Some(0) | None => 3,
        Some(code) => code,
    }
}

pub(super) fn is_missing_process_group(error: rustix::io::Errno) -> bool {
    error == rustix::io::Errno::SRCH
}
